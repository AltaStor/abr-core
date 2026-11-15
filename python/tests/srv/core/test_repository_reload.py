# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import copy
import re
import unittest
from unittest.mock import patch

from python.srv.core import job_scheduler, mongo_db_wrapper
from python.srv.core.job_scheduler import JobScheduler
from python.srv.core.mongo_db_wrapper import REPOSITORIES, SYSTEM_STATE


class _Database:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {}

    def _matches(self, row: dict, query: dict) -> bool:
        for key, wanted in query.items():
            if isinstance(wanted, dict) and "$regex" in wanted:
                if not re.search(wanted["$regex"], str(row.get(key) or "")):
                    return False
            elif row.get(key) != wanted:
                return False
        return True

    def find(self, collection: str, query: dict) -> list[dict]:
        return [copy.deepcopy(row) for row in self.collections.setdefault(collection, {}).values() if self._matches(row, query)]

    def get(self, collection: str, id_field: str, doc_id: str) -> dict | None:
        row = self.collections.setdefault(collection, {}).get(doc_id)
        return copy.deepcopy(row) if row else None

    def upsert(self, collection: str, id_field: str, document: dict) -> None:
        self.collections.setdefault(collection, {})[document[id_field]] = copy.deepcopy(document)


SHARED = {"id": "repo", "name": "Repo", "type": "smb", "path": "/srv/repo", "metadata": {"support_hyperv": True, "share": "abr-x$", "server": "host-1"}}


class RepositoryReloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _Database()
        handle = patch.object(mongo_db_wrapper, "get_database", lambda: self.db)
        handle.start()
        self.addCleanup(handle.stop)
        REPOSITORIES.clear()
        SYSTEM_STATE.clear()
        self.addCleanup(REPOSITORIES.clear)
        self.addCleanup(SYSTEM_STATE.clear)
        REPOSITORIES["repo"] = {"id": "repo", "name": "Repo", "type": "local", "path": "/srv/repo", "metadata": {}}
        self.scheduler = JobScheduler(poll_interval_seconds=0.1)

    def _script_shares_it(self, request_id: str) -> None:
        self.db.upsert("repositories", "id", SHARED)
        mongo_db_wrapper.request_repository_reload("repo", request_id)

    def test_a_change_the_script_asks_for_is_read_once_and_confirmed(self) -> None:
        self._script_shares_it("req-1")

        self.scheduler._apply_repository_reloads()

        self.assertEqual((REPOSITORIES["repo"]["type"], REPOSITORIES["repo"]["metadata"]["share"]), ("smb", "abr-x$"))
        self.assertEqual(mongo_db_wrapper.repository_reload_done("repo"), "req-1")
        with patch.object(job_scheduler, "reload_repository") as reload:
            self.scheduler._apply_repository_reloads()
        reload.assert_not_called()

    def test_a_newer_request_is_served_again(self) -> None:
        self._script_shares_it("req-1")
        self.scheduler._apply_repository_reloads()
        self.db.upsert("repositories", "id", {**SHARED, "type": "local", "metadata": {}})
        mongo_db_wrapper.request_repository_reload("repo", "req-2")

        self.scheduler._apply_repository_reloads()

        self.assertEqual((REPOSITORIES["repo"]["type"], mongo_db_wrapper.repository_reload_done("repo")), ("local", "req-2"))

    def test_a_repository_gone_from_the_catalog_is_dropped(self) -> None:
        mongo_db_wrapper.request_repository_reload("repo", "req-1")

        self.scheduler._apply_repository_reloads()

        self.assertNotIn("repo", REPOSITORIES)

    def test_after_the_reload_a_save_by_the_server_keeps_the_share(self) -> None:
        self._script_shares_it("req-1")
        self.scheduler._apply_repository_reloads()

        mongo_db_wrapper.persist_repository({**REPOSITORIES["repo"], "last_scan_at": "now"})

        self.assertEqual(self.db.get("repositories", "id", "repo")["metadata"]["share"], "abr-x$")

    def test_the_server_status_says_the_server_runs_and_reloads_and_is_written_at_most_every_few_seconds(self) -> None:
        with patch.object(job_scheduler, "record_server_status") as status:
            self.scheduler._apply_repository_reloads()
            self.scheduler._apply_repository_reloads()
        status.assert_called_once_with(running=True)

        mongo_db_wrapper.record_server_status(running=True)
        status = mongo_db_wrapper.server_status()
        self.assertEqual((status["running"], status["repository_reload"]), (True, True))
        self.assertTrue(status["pid"])

    def test_stopping_the_scheduler_marks_the_server_stopped(self) -> None:
        self.scheduler.stop()

        self.assertFalse(mongo_db_wrapper.server_status()["running"])


if __name__ == "__main__":
    unittest.main()
