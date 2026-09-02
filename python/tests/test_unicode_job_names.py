# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

"""Regression tests for non-Latin job-name handling.

Background
----------
Product-level create/edit validation currently accepts ASCII names only, while
the storage layer can still persist arbitrary UTF-8 strings without mangling
them. These tests keep both facts explicit: service-layer creation rejects
non-Latin names, and direct MongoDB persistence remains UTF-8 safe.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
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

REPO_ROOT = Path(__file__).resolve().parents[2]

# Names across several scripts that must survive create -> store -> read back
# completely unchanged. Each is distinct (job-name uniqueness is enforced).
UNICODE_NAMES: dict[str, str] = {
    "cyrillic": "Резервная копия БД 2026",
    "cyrillic_mixed_latin": "Backup-Сервер-01",
    "cjk": "夜間バックアップ",
    "greek": "Αντίγραφο ασφαλείας",
    "accented_latin": "Sauvegarde quotidienne — café",
    "emoji": "nightly 🌙 backup",
}
LATIN_NAME = "nightly-linux-backup"


class UnicodeJobNameServiceLayerTests(unittest.TestCase):
    """Drives the service layer (where any name validation would live) and reads
    back through the public list API. In-memory store, like the rest of the suite."""

    def setUp(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo One", "type": "local", "path": "/tmp/repo-1"}
        self.inventory = abr_v41.create_inventory_record(
            {
                "name": "vcenter-main",
                "type": "hyper-v",
                "address": "192.168.10.10",
                "username": "administrator",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[str(self.inventory["id"])] = {
            ("virtual_machine", "vm-101"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-101",
                "name": "web-01",
                "payload": {"path": "/Production/web-01"},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        }

    def _create_job(self, name: str) -> dict[str, object]:
        return abr_v41.create_backup_job(
            {
                "name": name,
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
            }
        )

    def test_non_latin_job_names_are_rejected(self) -> None:
        for label, name in UNICODE_NAMES.items():
            with self.subTest(label=label):
                with self.assertRaises(ApiError) as ctx:
                    self._create_job(name)
                self.assertEqual(ctx.exception.code, "invalid_job")
                self.assertEqual(ctx.exception.status_code, 400)

    def test_overlong_job_names_are_rejected(self) -> None:
        """Names over 125 characters are rejected with a 400, even if Latin."""
        with self.assertRaises(ApiError) as ctx:
            self._create_job("a" * 126)
        self.assertEqual(ctx.exception.code, "invalid_job")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_max_length_job_name_accepted(self) -> None:
        """A 125-character Latin name sits exactly on the boundary and is allowed."""
        name = "a" * 125
        job = self._create_job(name)
        self.assertEqual(job["name"], name)

    def test_latin_job_name_still_accepted(self) -> None:
        """No regression: ordinary Latin names keep working."""
        job = self._create_job(LATIN_NAME)
        self.assertEqual(job["name"], LATIN_NAME)
        names = {item["name"] for item in abr_v41.list_jobs()}
        self.assertIn(LATIN_NAME, names)

    def test_non_latin_job_description_is_rejected(self) -> None:
        """Validation is not limited to the name field: a Cyrillic description
        on an otherwise-valid job is rejected with a 400."""
        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_backup_job(
                {
                    "name": "nightly-with-bad-desc",
                    "vms": ["web-01"],
                    "inventory_id": self.inventory["id"],
                    "repo_id": "repo-1",
                    "description": "Резервная копия",
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_job")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_latin_punctuation_job_description_accepted(self) -> None:
        """Free-text descriptions keep accepting ordinary punctuation/symbols
        that the stricter name rule forbids."""
        job = abr_v41.create_backup_job(
            {
                "name": "nightly-with-desc",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "description": "Nightly @ 02:00 (prod / tier-1) -- keep 30d!",
            }
        )
        self.assertEqual(job["description"], "Nightly @ 02:00 (prod / tier-1) -- keep 30d!")


class UnicodeTextFieldValidationTests(unittest.TestCase):
    """Covers non-name text fields on inventory and repository create paths."""

    def setUp(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()

    def test_non_latin_inventory_credentials_are_accepted(self) -> None:
        record = abr_v41.create_inventory_record(
            {
                "name": "vcenter-main",
                "type": "hyper-v",
                "address": "192.168.10.10",
                "username": "адмінistrator@vsphere.local",
                "password": "пароль",
            }
        )
        self.assertEqual(record["username"], "адмінistrator@vsphere.local")

    def test_control_characters_in_inventory_credentials_are_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_inventory_record(
                {
                    "name": "vcenter-main",
                    "type": "hyper-v",
                    "address": "192.168.10.10",
                    "username": "administrator",
                    "password": "bad\x00null",
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_inventory")
        self.assertEqual(ctx.exception.status_code, 400)
        # Secret value must not be echoed back in the error details.
        self.assertNotIn("bad", str(ctx.exception.details))

    def test_non_latin_inventory_address_is_still_rejected(self) -> None:
        """Unlike credentials, the address is AltaStor's own connection
        target, not a customer account -- it keeps the stricter Latin-only
        rule."""
        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_inventory_record(
                {
                    "name": "vcenter-main",
                    "type": "hyper-v",
                    "address": "вцентр.local",
                    "username": "administrator",
                    "password": "secret",
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_inventory")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_latin_repository_description_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_repository_record(
                {
                    "name": "repo-one",
                    "type": "local",
                    "path": "/tmp/repo-1",
                    "description": "Хранилище",
                }
            )
        self.assertEqual(ctx.exception.code, "invalid_repository")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_latin_repository_with_pathlike_fields_accepted(self) -> None:
        """No regression: paths/hosts with dots and slashes still pass."""
        with tempfile.TemporaryDirectory(suffix="-tier-1.local") as tmpdir:
            repo = abr_v41.create_repository_record(
                {
                    "name": "repo-one",
                    "type": "local",
                    "path": tmpdir,
                    "description": "primary repo",
                }
            )
            self.assertEqual(repo["name"], "repo-one")


def _find_mongod() -> str | None:
    bundled = REPO_ROOT / "tools" / "mongodb" / "bin" / "mongod"
    if bundled.exists():
        return str(bundled)
    from shutil import which

    return which("mongod")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@unittest.skipIf(_find_mongod() is None, "mongod binary not available")
class UnicodeJobNameMongoRoundTripTests(unittest.TestCase):
    """Proves the persistence layer stores/loads Unicode names intact against a
    real MongoDB — the exact claim the ticket said was broken."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="br-mongo-unicode-")
        self._port = _free_port()
        self._proc = subprocess.Popen(
            [
                _find_mongod(),
                "--dbpath", self._tmpdir,
                "--bind_ip", "127.0.0.1",
                "--port", str(self._port),
                "--quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._prev_uri = os.environ.get("MONGODB_URI")
        self._prev_db = os.environ.get("MONGODB_DB_NAME")
        os.environ["MONGODB_URI"] = f"mongodb://127.0.0.1:{self._port}"
        os.environ["MONGODB_DB_NAME"] = "unicode_roundtrip_test"

        # Reset the cached connection so it picks up the test instance, and wait
        # until the server accepts connections.
        from python.srv.core import mongo_db_wrapper as wrapper

        self._wrapper = wrapper
        wrapper.close_database()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if wrapper.get_database() is not None:
                break
            time.sleep(0.2)
        else:
            self._stop_mongo()
            self.skipTest("could not start a local MongoDB for the round-trip test")

    def _stop_mongo(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=10)

    def tearDown(self) -> None:
        try:
            self._wrapper.close_database()
        finally:
            self._stop_mongo()
            if self._prev_uri is None:
                os.environ.pop("MONGODB_URI", None)
            else:
                os.environ["MONGODB_URI"] = self._prev_uri
            if self._prev_db is None:
                os.environ.pop("MONGODB_DB_NAME", None)
            else:
                os.environ["MONGODB_DB_NAME"] = self._prev_db
            import shutil

            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_unicode_job_names_persist_and_reload_unchanged(self) -> None:
        wrapper = self._wrapper
        database = wrapper.get_database()
        self.assertIsNotNone(database, "expected a live MongoDB connection")

        ids: dict[str, str] = {}
        try:
            for label, name in UNICODE_NAMES.items():
                job_id = f"unicode-{label}-{uuid.uuid4().hex}"
                ids[job_id] = name
                wrapper.persist_job({"id": job_id, "name": name, "metadata": {"name": name}})

            # Read back through the public load path...
            loaded = wrapper.load_jobs()
            for job_id, name in ids.items():
                self.assertIn(job_id, loaded)
                self.assertEqual(loaded[job_id]["name"], name, f"load_jobs mangled {job_id}")

            # ...and verify what is physically stored is a proper str, not bytes
            # or an escaped sequence.
            for job_id, name in ids.items():
                raw = database.get("jobs", "id", job_id)
                self.assertIsNotNone(raw)
                self.assertIsInstance(raw["name"], str)
                self.assertEqual(raw["name"], name)
        finally:
            for job_id in ids:
                wrapper.delete_job(job_id)


if __name__ == "__main__":
    unittest.main()
