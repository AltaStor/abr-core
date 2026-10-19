# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import pathlib
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

from pymongo.errors import DuplicateKeyError, PyMongoError

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    JOBS,
    REPOSITORIES,
    TASKS,
    catalog_entries,
    catalog_records,
    delete_repository,
    persist_repository,
)
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError

CHURN_SECONDS = 2.0
FILLER_RECORDS = 200
BACKUP_RECORDS = 40


def _repository(record_id: str, path: str) -> dict[str, object]:
    return {
        "id": record_id,
        "name": record_id,
        "type": "local",
        "status": "available",
        "path": path,
        "metadata": {"address": path, "description": "", "last_scan_at": None},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


class CatalogRaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()
        JOBS.clear()
        TASKS.clear()
        BACKUPS.clear()
        for index in range(FILLER_RECORDS):
            REPOSITORIES[f"filler-{index}"] = _repository(f"filler-{index}", f"/srv/filler/{index}")
            JOBS[f"job-{index}"] = {
                "id": f"job-{index}",
                "name": f"job-{index}",
                "type": "backup",
                "status": "active",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            TASKS[f"task-{index}"] = {
                "id": f"task-{index}",
                "job_id": f"job-{index}",
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        for index in range(BACKUP_RECORDS):
            BACKUPS[f"backup-{index}"] = {
                "id": f"backup-{index}",
                "origin": {"job_id": f"job-{index}"},
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        self._stop = threading.Event()
        self._failures: list[BaseException] = []
        self._lock = threading.Lock()
        self.reads = 0

    def tearDown(self) -> None:
        self._stop.set()
        REPOSITORIES.clear()
        JOBS.clear()
        TASKS.clear()
        BACKUPS.clear()

    def _churn(self) -> None:
        index = 0
        while not self._stop.is_set():
            record_id = f"churn-{threading.get_ident()}-{index}"
            persist_repository(_repository(record_id, f"/srv/churn/{index}"))
            time.sleep(0.002)
            delete_repository(record_id)
            time.sleep(0.002)
            index += 1

    def _read(self, action) -> None:
        while not self._stop.is_set():
            try:
                action()
                with self._lock:
                    self.reads += 1
            except ApiError:
                pass
            except BaseException as exc:
                with self._lock:
                    self._failures.append(exc)

    def _run(self, actions, *, writers: int = 3) -> None:
        threads = [threading.Thread(target=self._churn, daemon=True) for _ in range(writers)]
        threads += [threading.Thread(target=self._read, args=(action,), daemon=True) for action in actions]
        for thread in threads:
            thread.start()
        time.sleep(CHURN_SECONDS)
        self._stop.set()
        for thread in threads:
            thread.join(timeout=5)

    def assertNoFailures(self) -> None:
        self.assertGreater(self.reads, 0, "the readers never completed a single pass")
        if self._failures:
            summary = "\n".join(f"{type(e).__name__}: {e}" for e in self._failures[:5])
            self.fail(f"{len(self._failures)} concurrent read(s) failed:\n{summary}")


class ConcurrentCatalogReadTests(CatalogRaceTestCase):
    def test_repository_listing_survives_concurrent_writes(self) -> None:
        self._run([abr_v41.list_repository_records] * 3)
        self.assertNoFailures()

    def test_overlap_check_survives_concurrent_writes(self) -> None:
        self._run([lambda: abr_v41._find_overlapping_repository("/srv/filler/7/nested")] * 3)
        self.assertNoFailures()

    def test_job_and_session_listings_survive_concurrent_writes(self) -> None:
        self._run([abr_v41.normalized_job_records, abr_v41.list_sessions, abr_v41._backup_points_raw])
        self.assertNoFailures()

    def test_repository_creation_refuses_overlaps_with_409_not_500(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            nested = pathlib.Path(outer) / "nested"
            nested.mkdir()
            REPOSITORIES["outer"] = _repository("outer", outer)
            refusals = {"count": 0}

            def overlapping_add() -> None:
                try:
                    abr_v41.create_repository_record(
                        {"name": f"overlap-{threading.get_ident()}", "type": "local", "path": str(nested)}
                    )
                except ApiError as exc:
                    self.assertEqual(exc.status_code, 409)
                    self.assertEqual(exc.code, "repository_path_overlaps")
                    with self._lock:
                        refusals["count"] += 1
                    return
                self.fail("an overlapping repository was accepted")

            self._run([overlapping_add] * 4)
            self.assertNoFailures()
            self.assertGreater(refusals["count"], 0)


class PersistenceOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()

    def tearDown(self) -> None:
        REPOSITORIES.clear()

    def _failing_db(self, *, fails: str) -> mock.Mock:
        db = mock.Mock()
        getattr(db, fails).side_effect = PyMongoError("Cannot use MongoClient after close")
        return db

    def _conflicting_db(self, *, fails: str, key_value: dict[str, object]) -> mock.Mock:
        db = mock.Mock()
        getattr(db, fails).side_effect = DuplicateKeyError(
            "E11000 duplicate key error", 11000, {"keyValue": key_value}
        )
        return db

    def test_persist_repository_leaves_no_phantom_when_the_db_write_fails(self) -> None:
        record = _repository("phantom", "/srv/phantom")
        with mock.patch(
            "python.srv.core.mongo_db_wrapper.get_database",
            return_value=self._failing_db(fails="upsert"),
        ), self.assertRaises(ApiError) as ctx:
            persist_repository(record)

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.code, "catalog_unavailable")
        self.assertNotIn("phantom", REPOSITORIES)

    def test_delete_repository_keeps_the_record_when_the_db_write_fails(self) -> None:
        REPOSITORIES["stubborn"] = _repository("stubborn", "/srv/stubborn")
        with mock.patch(
            "python.srv.core.mongo_db_wrapper.get_database",
            return_value=self._failing_db(fails="delete"),
        ), self.assertRaises(ApiError) as ctx:
            delete_repository("stubborn")

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("stubborn", REPOSITORIES)

    def test_create_repository_record_refuses_cleanly_when_the_catalog_db_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "python.srv.core.mongo_db_wrapper.get_database",
            return_value=self._failing_db(fails="upsert"),
        ), self.assertRaises(ApiError) as ctx:
            abr_v41.create_repository_record({"name": "outage-probe", "type": "local", "path": tmp})

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.code, "catalog_unavailable")
        self.assertFalse(
            any(repo.get("name") == "outage-probe" for repo in REPOSITORIES.values()),
            "the repository was left registered in memory despite the failed persist",
        )

    def test_a_racing_uniqueness_conflict_is_reported_as_a_conflict_not_an_outage(self) -> None:
        record = _repository("conflict", "/srv/conflict")
        with mock.patch(
            "python.srv.core.mongo_db_wrapper.get_database",
            return_value=self._conflicting_db(fails="upsert", key_value={"name": "conflict"}),
        ), self.assertRaises(ApiError) as ctx:
            persist_repository(record)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.code, "catalog_conflict")
        self.assertNotIn("conflict", REPOSITORIES)


class CatalogSnapshotHelperTests(unittest.TestCase):
    def test_snapshot_is_detached_from_later_writes(self) -> None:
        mapping = {"a": {"id": "a"}}
        snapshot = catalog_records(mapping)
        mapping["b"] = {"id": "b"}
        self.assertEqual([record["id"] for record in snapshot], ["a"])

    def test_entries_snapshot_keeps_the_keys(self) -> None:
        self.assertEqual(catalog_entries({"a": {"id": "a"}}), [("a", {"id": "a"})])

    def test_records_are_shared_not_copied(self) -> None:
        record = {"id": "a"}
        self.assertIs(catalog_records({"a": record})[0], record)


class CatalogReadDisciplineTests(unittest.TestCase):
    CATALOG_MAPS = ("REPOSITORIES", "HYPERVISORS", "JOBS", "BACKUPS", "TASKS", "USERS", "SESSIONS")

    def test_no_module_outside_the_wrapper_iterates_a_catalog_map(self) -> None:
        server_root = pathlib.Path(__file__).resolve().parents[3] / "srv"
        pattern = re.compile(rf"\b({'|'.join(self.CATALOG_MAPS)})\.(items|values|keys)\(\)")
        offenders = []
        for source in server_root.rglob("*.py"):
            if source.name == "mongo_db_wrapper.py":
                continue
            for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{source.relative_to(server_root.parent)}:{number}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "iterate a snapshot from catalog_records()/catalog_entries() instead:\n"
            + "\n".join(offenders),
        )


class UnhandledErrorReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        from python.srv.api.auth_guard import require_session
        from python.srv.backup_server import app

        self.app = app
        self.app.dependency_overrides[require_session] = lambda: {
            "token": "test", "user_id": "test", "user_name": "test", "role": "administrator"
        }
        self.addCleanup(self.app.dependency_overrides.clear)

    def _get_repositories_with_a_broken_reader(self):
        from fastapi.testclient import TestClient

        with mock.patch.object(
            abr_v41, "list_repository_records", side_effect=RuntimeError("dictionary changed size during iteration")
        ):
            with TestClient(self.app, raise_server_exceptions=False) as client:
                return client.get("/api/v1/repositories")

    def test_an_unhandled_error_answers_with_a_typed_body(self) -> None:
        with self.assertLogs("python.srv.backup_server", level="ERROR"):
            response = self._get_repositories_with_a_broken_reader()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")

    def test_an_unhandled_error_reaches_the_product_log_with_its_traceback(self) -> None:
        with self.assertLogs("python.srv.backup_server", level="ERROR") as captured:
            self._get_repositories_with_a_broken_reader()

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertIsNotNone(record.exc_info, "the traceback was not attached to the log record")
        self.assertIn("/api/v1/repositories", record.getMessage())
        self.assertIn("dictionary changed size during iteration", "".join(captured.output))


if __name__ == "__main__":
    unittest.main()
