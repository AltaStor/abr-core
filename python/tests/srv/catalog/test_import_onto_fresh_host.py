# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pymongo.errors import DuplicateKeyError

from python.srv.core import credential_encryption, mongo_db_wrapper
from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, SESSIONS, TASKS, USERS
from python.srv.services.api_errors import ApiError

VCENTER_PASSWORD = "Vc-Admin-Secret!42"


class _MongoLikeDatabase:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {}
        self.writes: list[tuple[str, str]] = []

    def find(self, collection: str, query: dict) -> list[dict]:
        rows = self.collections.setdefault(collection, {}).values()
        return [copy.deepcopy(row) for row in rows if all(row.get(k) == v for k, v in query.items())]

    def upsert(self, collection: str, id_field: str, document: dict) -> None:
        rows = self.collections.setdefault(collection, {})
        if collection == "users":
            for row in rows.values():
                if row.get("name") == document.get("name") and row.get("id") != document.get("id"):
                    raise DuplicateKeyError("E11000", details={"keyValue": {"name": document.get("name")}})
        self.writes.append((collection, str(document[id_field])))
        rows[document[id_field]] = copy.deepcopy(document)

    def delete(self, collection: str, id_field: str, doc_id: str) -> bool:
        self.writes.append((f"delete:{collection}", str(doc_id)))
        return self.collections.setdefault(collection, {}).pop(doc_id, None) is not None

    def replace_hypervisor_resources(self, hypervisor_id: str, resources: list[dict]) -> None:
        self.writes.append(("resources", hypervisor_id))
        self.collections.setdefault("resources", {})[hypervisor_id] = {"items": copy.deepcopy(resources)}

    def get_queued_tasks(self) -> list[dict]:
        return []


def _exported_catalog() -> dict:
    return {
        "hypervisors": {
            "hv1": {
                "id": "hv1",
                "name": "vCenter-Prod",
                "metadata": {"host": "vc.local", "credentials": {"username": "administrator@vsphere.local", "password": VCENTER_PASSWORD}},
            }
        },
        "repositories": {"repo1": {"id": "repo1", "name": "local-repo-1", "type": "local"}},
        "jobs": {"job1": {"id": "job1", "name": "nightly"}},
        "backups": {"bkp1": {"id": "bkp1", "job_id": "job1", "repository_id": "repo1"}},
        "tasks": {"task1": {"id": "task1", "task_type": "backup", "status": "completed"}},
        "users": {
            "old-admin": {"id": "old-admin", "name": "admin", "passwd": "old-server-hash"},
            "old-alice": {"id": "old-alice", "name": "alice", "passwd": "alice-hash"},
        },
        "resources": {"hv1": [{"resource_key": "hv1:vm-1", "resource_type": "virtual_machine", "external_id": "vm-1"}]},
    }


class ImportOntoAFreshHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.key_path = Path(self._tmp.name) / "credentials.key"
        env = patch.dict(os.environ, {credential_encryption.KEY_FILE_ENV: str(self.key_path)})
        env.start()
        self.addCleanup(env.stop)
        credential_encryption.reset_cipher()
        self.addCleanup(credential_encryption.reset_cipher)
        for store in (HYPERVISORS, REPOSITORIES, JOBS, BACKUPS, TASKS, USERS, SESSIONS):
            store.clear()
            self.addCleanup(store.clear)
        self.db = _MongoLikeDatabase()
        handle = patch.object(mongo_db_wrapper, "get_database", lambda: self.db)
        handle.start()
        self.addCleanup(handle.stop)
        mongo_db_wrapper.persist_user({"id": "bootstrap-admin", "name": "admin", "passwd": "bootstrap-hash"})
        mongo_db_wrapper.persist_session({"token": "operator-token", "user_id": "bootstrap-admin", "expires_at": "2999-01-01T00:00:00+00:00"})
        self.db.writes.clear()

    def test_the_exported_admin_replaces_the_bootstrap_admin_and_everything_is_restored(self) -> None:
        catalog = _exported_catalog()

        replaced = mongo_db_wrapper.users_replaced_by_import(catalog)
        counts = mongo_db_wrapper.restore_catalog(catalog)

        self.assertEqual([user["id"] for user in replaced], ["bootstrap-admin"])
        self.assertEqual(sorted(USERS), ["old-admin", "old-alice"])
        self.assertEqual(sorted(self.db.collections["users"]), ["old-admin", "old-alice"])
        self.assertEqual(self.db.collections["users"]["old-admin"]["passwd"], "old-server-hash")
        self.assertEqual(SESSIONS, {})
        self.assertEqual(self.db.collections.get("sessions", {}), {})
        self.assertEqual(counts, {"roles": 0, "users": 2, "hypervisors": 1, "repositories": 1, "jobs": 1, "backups": 1, "tasks": 1, "resources": 1})
        self.assertIn("hv1", self.db.collections["resources"])
        stored = self.db.collections["hypervisors"]["hv1"]["metadata"]["credentials"]["password"]
        self.assertTrue(credential_encryption.is_encrypted(stored))

    def test_users_are_written_before_anything_else(self) -> None:
        mongo_db_wrapper.restore_catalog(_exported_catalog())

        first_other = next(i for i, (collection, _id) in enumerate(self.db.writes) if not collection.endswith("users") and "sessions" not in collection)
        last_user = max(i for i, (collection, _id) in enumerate(self.db.writes) if collection == "users")
        self.assertLess(last_user, first_other)

    def test_a_user_the_export_does_not_name_is_kept(self) -> None:
        mongo_db_wrapper.persist_user({"id": "local-ops", "name": "ops", "passwd": "ops-hash"})

        mongo_db_wrapper.restore_catalog(_exported_catalog())

        self.assertIn("local-ops", USERS)
        self.assertIn("local-ops", self.db.collections["users"])

    def test_importing_the_same_export_again_replaces_nobody(self) -> None:
        catalog = _exported_catalog()
        mongo_db_wrapper.restore_catalog(catalog)

        self.assertEqual(mongo_db_wrapper.users_replaced_by_import(catalog), [])
        mongo_db_wrapper.restore_catalog(catalog)
        self.assertEqual(sorted(USERS), ["old-admin", "old-alice"])

    def test_an_export_with_two_users_of_one_name_is_refused_before_anything_is_written(self) -> None:
        catalog = _exported_catalog()
        catalog["users"]["dup"] = {"id": "dup", "name": "alice", "passwd": "x"}

        with self.assertRaises(ApiError) as raised:
            mongo_db_wrapper.restore_catalog(catalog)

        self.assertEqual((raised.exception.status_code, raised.exception.code), (422, "invalid_export"))
        self.assertEqual(self.db.writes, [])
        self.assertEqual(sorted(USERS), ["bootstrap-admin"])

    def test_an_unusable_credential_key_refuses_the_import_before_anything_is_written(self) -> None:
        self.key_path.write_bytes(b"not-a-fernet-key")
        credential_encryption.reset_cipher()

        with self.assertRaises(ApiError) as raised:
            mongo_db_wrapper.restore_catalog(_exported_catalog())

        self.assertEqual((raised.exception.status_code, raised.exception.code), (503, "credential_key_unavailable"))
        self.assertEqual(self.db.writes, [])
        self.assertEqual(sorted(USERS), ["bootstrap-admin"])
        self.assertEqual(len(SESSIONS), 1)


if __name__ == "__main__":
    unittest.main()
