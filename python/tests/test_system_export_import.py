# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi.testclient import TestClient

from python.srv.api.auth_guard import require_session
from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    SESSIONS,
    TASKS,
    USERS,
)
from python.srv.core.secure_archive import (
    ArchiveError,
    WrongPasswordError,
    decrypt_payload,
    encrypt_payload,
)

PASSWORD = "correct horse battery staple"


def _reset_catalog() -> None:
    BACKUPS.clear()
    HYPERVISORS.clear()
    JOBS.clear()
    REPOSITORIES.clear()
    RESOURCES_BY_HV.clear()
    TASKS.clear()
    USERS.clear()
    SESSIONS.clear()


class SecureArchiveTests(unittest.TestCase):
    def test_round_trip_recovers_original_payload(self) -> None:
        payload = {
            "manifest": {"format_version": 1},
            "catalog": {"repositories": {"r1": {"s3_secret_key": "AKIA_SECRET"}}},
        }
        blob = encrypt_payload(payload, "correct horse battery staple")
        self.assertEqual(decrypt_payload(blob, "correct horse battery staple"), payload)

    def test_secrets_are_not_visible_in_ciphertext(self) -> None:
        payload = {
            "catalog": {
                "repositories": {"r1": {"s3_secret_key": "AKIA_VERY_SECRET_VALUE"}}
            }
        }
        blob = encrypt_payload(payload, "pw")
        self.assertNotIn(b"AKIA_VERY_SECRET_VALUE", blob)

    def test_wrong_password_is_rejected(self) -> None:
        blob = encrypt_payload({"a": 1}, "right-password")
        with self.assertRaises(WrongPasswordError):
            decrypt_payload(blob, "wrong-password")

    def test_tampered_archive_is_rejected(self) -> None:
        blob = bytearray(encrypt_payload({"a": 1}, "pw"))
        blob[-1] ^= 0xFF
        with self.assertRaises(WrongPasswordError):
            decrypt_payload(bytes(blob), "pw")

    def test_truncated_file_raises_archive_error(self) -> None:
        with self.assertRaises(ArchiveError):
            decrypt_payload(b"not an archive", "pw")

    def test_empty_password_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            encrypt_payload({"a": 1}, "")


class SystemCatalogSnapshotApiTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_catalog()
        app.dependency_overrides[require_session] = lambda: {"user_id": "test-user", "user_name": "test-user"}
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        app.dependency_overrides.pop(require_session, None)
        _reset_catalog()

    def _seed_catalog(self) -> None:
        HYPERVISORS["hv1"] = {
            "id": "hv1",
            "name": "vcenter1",
            "metadata": {"credentials": {"password": "hv-secret"}},
        }
        REPOSITORIES["repo1"] = {
            "id": "repo1",
            "name": "s3repo",
            "s3_secret_key": "AKIA_SECRET",
        }
        JOBS["job1"] = {"id": "job1", "name": "nightly"}
        BACKUPS["bkp1"] = {
            "id": "bkp1",
            "job_id": "job1",
            "repository_id": "repo1",
            "size": 123,
        }
        TASKS["task1"] = {"id": "task1", "type": "backup", "status": "completed"}
        RESOURCES_BY_HV["hv1"] = {
            ("vm", "vm-1"): {
                "resource_type": "vm",
                "external_id": "vm-1",
                "name": "VM1",
            }
        }

    def _export(self, *, password: str = PASSWORD):
        return self.client.post(
            "/api/v1/system/catalog-snapshot", json={"password": password}
        )

    def _decrypted_export(self, *, password: str = PASSWORD):
        response = self._export(password=password)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        return body, decrypt_payload(base64.b64decode(body["archive_base64"]), password)

    def _restore(self, archive_base64: str, *, password: str = PASSWORD):
        return self.client.post(
            "/api/v1/system/catalog-restore",
            json={"password": password, "archive_base64": archive_base64},
        )

    def test_export_snapshot_contains_manifest_and_full_catalog(self) -> None:
        self._seed_catalog()

        body, payload = self._decrypted_export()

        self.assertEqual(body["manifest"]["format_version"], 1)
        self.assertEqual(payload["catalog"]["backups"]["bkp1"]["id"], "bkp1")
        self.assertEqual(
            payload["catalog"]["repositories"]["repo1"]["s3_secret_key"], "AKIA_SECRET"
        )

    def test_export_snapshot_password_never_leaves_a_trace_in_the_response(self) -> None:
        self._seed_catalog()

        response = self._export(password="super-secret-dr-password")

        self.assertNotIn(b"super-secret-dr-password", response.content)

    def test_export_then_import_on_clean_install_restores_catalog(self) -> None:
        self._seed_catalog()

        export_response = self._export(password="dr-password-123")
        self.assertEqual(export_response.status_code, 200)
        archive_base64 = export_response.json()["archive_base64"]

        with tempfile.TemporaryDirectory() as tmp:
            archive_path = pathlib.Path(tmp) / "dr-export.abrx"
            archive_path.write_bytes(base64.b64decode(archive_base64))

            _reset_catalog()

            self.assertEqual(BACKUPS, {})
            self.assertEqual(REPOSITORIES, {})

            archive_base64 = base64.b64encode(archive_path.read_bytes()).decode("ascii")

        import_response = self._restore(archive_base64, password="dr-password-123")
        self.assertEqual(import_response.status_code, 200)

        restored_counts = import_response.json()["restored"]

        self.assertEqual(restored_counts["hypervisors"], 1)
        self.assertEqual(restored_counts["repositories"], 1)
        self.assertEqual(restored_counts["jobs"], 1)
        self.assertEqual(restored_counts["backups"], 1)
        self.assertEqual(restored_counts["tasks"], 1)
        self.assertEqual(restored_counts["resources"], 1)

        restore_points_response = self.client.get("/api/v1/restore/points")
        self.assertEqual(restore_points_response.status_code, 200)
        restored_ids = {
            point["id"] for point in restore_points_response.json()["items"]
        }
        self.assertIn("bkp1", restored_ids)

        self.assertEqual(REPOSITORIES["repo1"]["s3_secret_key"], "AKIA_SECRET")
        self.assertEqual(HYPERVISORS["hv1"]["name"], "vcenter1")

    def test_export_snapshot_includes_allowlisted_configuration(self) -> None:
        self._seed_catalog()
        with patch.dict(
            "os.environ",
            {
                "MONGODB_URI": "mongodb://127.0.0.1:27018",
                "BACKUP_WORKER_COUNT": "4",
                "SOME_UNRELATED_VAR": "should-not-appear",
            },
        ):
            _, payload = self._decrypted_export()
        self.assertEqual(
            payload["configuration"]["MONGODB_URI"], "mongodb://127.0.0.1:27018"
        )
        self.assertEqual(payload["configuration"]["BACKUP_WORKER_COUNT"], "4")
        self.assertNotIn("SOME_UNRELATED_VAR", payload["configuration"])

    def test_a_restore_answer_masks_secrets_the_archive_still_carries(self) -> None:
        self._seed_catalog()
        with patch.dict(
            "os.environ",
            {
                "ABR_BOOTSTRAP_ADMIN_PASSWORD": "Bootstrap-Secret-77",
                "MONGODB_URI": "mongodb://catalog:Db-Secret-88@db.local:27017/backup_db",
                "BACKUP_WORKER_COUNT": "4",
            },
        ):
            body, payload = self._decrypted_export()

        restored = self._restore(body["archive_base64"]).json()
        configuration = restored["configuration"]

        self.assertEqual(configuration["ABR_BOOTSTRAP_ADMIN_PASSWORD"], "********")
        self.assertEqual(configuration["MONGODB_URI"], "mongodb://********@db.local:27017/backup_db")
        self.assertEqual(configuration["BACKUP_WORKER_COUNT"], "4")
        self.assertNotIn("Bootstrap-Secret-77", str(restored))
        self.assertNotIn("Db-Secret-88", str(restored))
        self.assertEqual(payload["configuration"]["ABR_BOOTSTRAP_ADMIN_PASSWORD"], "Bootstrap-Secret-77")
        self.assertEqual(
            payload["configuration"]["MONGODB_URI"], "mongodb://catalog:Db-Secret-88@db.local:27017/backup_db"
        )

    def test_export_snapshot_includes_server_process_configuration(self) -> None:
        self._seed_catalog()
        with patch.dict(
            "os.environ",
            {
                "ALTASTOR_BACKUP_APP": "python.srv.backup_server:app",
                "ALTASTOR_BACKUP_HOST": "0.0.0.0",
                "ALTASTOR_BACKUP_PORT": "9443",
            },
        ):
            _, payload = self._decrypted_export()

        self.assertEqual(payload["configuration"]["ALTASTOR_BACKUP_PORT"], "9443")
        self.assertEqual(payload["configuration"]["ALTASTOR_BACKUP_HOST"], "0.0.0.0")
        self.assertEqual(
            payload["configuration"]["ALTASTOR_BACKUP_APP"],
            "python.srv.backup_server:app",
        )

    def test_export_snapshot_includes_auth_configuration(self) -> None:
        self._seed_catalog()
        with patch.dict(
            "os.environ",
            {
                "ABR_BOOTSTRAP_ADMIN_USER": "abr",
                "ABR_SESSION_TTL_SECONDS": "300",
                "ABR_LOGIN_MAX_ATTEMPTS": "5",
                "ABR_LOGIN_LOCKOUT_SECONDS": "300",
            },
        ):
            _, payload = self._decrypted_export()
        configuration = payload["configuration"]
        self.assertEqual(configuration["ABR_BOOTSTRAP_ADMIN_USER"], "abr")
        self.assertEqual(configuration["ABR_SESSION_TTL_SECONDS"], "300")
        self.assertEqual(configuration["ABR_LOGIN_MAX_ATTEMPTS"], "5")
        self.assertEqual(configuration["ABR_LOGIN_LOCKOUT_SECONDS"], "300")

    def test_export_snapshot_includes_users_but_excludes_live_sessions(self) -> None:
        USERS["user1"] = {"id": "user1", "name": "alice", "passwd": "deadbeef", "fullName": "Alice"}
        SESSIONS["tok1"] = {"token": "tok1", "user_id": "user1", "expires_at": "2099-01-01T00:00:00+00:00"}

        _, payload = self._decrypted_export()
        catalog = payload["catalog"]

        self.assertEqual(catalog["users"]["user1"]["name"], "alice")
        self.assertNotIn("sessions", catalog)

    def test_import_restores_users(self) -> None:
        USERS.clear()
        USERS["user1"] = {"id": "user1", "name": "alice", "passwd": "deadbeef", "fullName": "Alice"}

        export_response = self._export()
        archive_base64 = export_response.json()["archive_base64"]

        _reset_catalog()
        self.assertEqual(USERS, {})

        import_response = self._restore(archive_base64)
        self.assertEqual(import_response.status_code, 200)
        self.assertEqual(import_response.json()["restored"]["users"], 1)
        self.assertEqual(USERS["user1"]["name"], "alice")

    def test_import_onto_a_fresh_install_replaces_its_bootstrap_admin(self) -> None:
        USERS.clear()
        USERS["old-admin"] = {"id": "old-admin", "name": "admin", "passwd": "old-server-hash"}
        archive_base64 = self._export().json()["archive_base64"]
        _reset_catalog()
        USERS["bootstrap-admin"] = {"id": "bootstrap-admin", "name": "admin", "passwd": "bootstrap-hash"}

        import_response = self._restore(archive_base64)

        self.assertEqual(import_response.status_code, 200, import_response.text)
        self.assertEqual(import_response.json()["replaced_users"], ["admin"])
        self.assertEqual(list(USERS), ["old-admin"])

    def test_import_onto_the_same_server_replaces_nobody(self) -> None:
        USERS.clear()
        USERS["user1"] = {"id": "user1", "name": "alice", "passwd": "deadbeef"}
        archive_base64 = self._export().json()["archive_base64"]

        import_response = self._restore(archive_base64)

        self.assertEqual(import_response.status_code, 200, import_response.text)
        self.assertEqual(import_response.json()["replaced_users"], [])
        self.assertEqual(list(USERS), ["user1"])

    def test_export_falls_back_to_server_defaults_file_when_not_in_process_env(
        self,
    ) -> None:
        self._seed_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            defaults_file = pathlib.Path(tmp) / "altastor-backup-server.default"
            defaults_file.write_text(
                "\n".join(
                    [
                        "PYTHONPATH=/opt/altastor-backup-server",
                        "ALTASTOR_BACKUP_APP=python.srv.backup_server:app",
                        "ALTASTOR_BACKUP_PORT=9443",
                        "MONGODB_URI=mongodb://127.0.0.1:27018",
                        "BACKUPSERVER_STORAGE_ROOT=/var/lib/altastor-backup/storage",
                        "# a comment line",
                        "BACKUP_WORKER_COUNT=4",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"ABR_SERVER_DEFAULTS_FILE": str(defaults_file)},
                clear=False,
            ):
                for key in (
                    "ALTASTOR_BACKUP_PORT",
                    "MONGODB_URI",
                    "BACKUPSERVER_STORAGE_ROOT",
                    "BACKUP_WORKER_COUNT",
                ):
                    os.environ.pop(key, None)
                _, payload = self._decrypted_export()

        self.assertEqual(payload["configuration"]["ALTASTOR_BACKUP_PORT"], "9443")
        self.assertEqual(
            payload["configuration"]["MONGODB_URI"], "mongodb://127.0.0.1:27018"
        )
        self.assertEqual(
            payload["configuration"]["BACKUPSERVER_STORAGE_ROOT"],
            "/var/lib/altastor-backup/storage",
        )
        self.assertEqual(payload["configuration"]["BACKUP_WORKER_COUNT"], "4")
        self.assertNotIn("PYTHONPATH", payload["configuration"])

    def test_export_prefers_live_process_env_over_defaults_file(self) -> None:
        self._seed_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            defaults_file = pathlib.Path(tmp) / "altastor-backup-server.default"
            defaults_file.write_text("ALTASTOR_BACKUP_PORT=8000\n", encoding="utf-8")
            with patch.dict(
                "os.environ",
                {
                    "ABR_SERVER_DEFAULTS_FILE": str(defaults_file),
                    "ALTASTOR_BACKUP_PORT": "9443",
                },
            ):
                _, payload = self._decrypted_export()

        self.assertEqual(payload["configuration"]["ALTASTOR_BACKUP_PORT"], "9443")

    def test_export_snapshot_is_never_torn_under_concurrent_writes(self) -> None:
        import threading

        from python.srv.core.mongo_db_wrapper import persist_job, persist_task

        self._seed_catalog()
        stop = threading.Event()
        errors: list[str] = []

        def writer(worker_id: int) -> None:
            i = 0

            while not stop.is_set():
                job_id = f"race-job-{worker_id}-{i % 5}"
                persist_job(
                    {
                        "id": job_id,
                        "name": f"race-{worker_id}-{i}",
                        "schedule": {"kind": "manual"},
                    }
                )
                persist_task(
                    {
                        "id": f"race-task-{worker_id}-{i % 5}",
                        "type": "backup",
                        "status": "running",
                    }
                )
                i += 1

        threads = [
            threading.Thread(target=writer, args=(w,), daemon=True) for w in range(4)
        ]
        for t in threads:
            t.start()

        try:
            for _ in range(10):
                response = self._export()
                if response.status_code != 200:
                    errors.append(
                        f"unexpected status {response.status_code}: {response.text}"
                    )
                    continue
                body = response.json()
                payload = decrypt_payload(
                    base64.b64decode(body["archive_base64"]), PASSWORD
                )
                catalog = payload["catalog"]
                manifest_counts = body["manifest"]["counts"]

                for table in (
                    "jobs",
                    "tasks",
                    "hypervisors",
                    "repositories",
                    "backups",
                ):
                    actual = len(catalog.get(table) or {})
                    reported = manifest_counts.get(table)
                    if actual != reported:
                        errors.append(
                            f"{table}: manifest says {reported}, catalog actually has {actual}"
                        )

                for job_id, job in catalog["jobs"].items():
                    if "id" not in job or "name" not in job:
                        errors.append(f"torn job record: {job_id} -> {job}")
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=5)

        self.assertEqual(errors, [])

    def test_import_returns_configuration_for_the_cli_to_write_out(self) -> None:
        self._seed_catalog()

        with patch.dict("os.environ", {"MONGODB_URI": "mongodb://127.0.0.1:27018"}):
            archive_base64 = self._export().json()["archive_base64"]
        _reset_catalog()

        import_response = self._restore(archive_base64)
        self.assertEqual(import_response.status_code, 200)
        self.assertEqual(
            import_response.json()["configuration"]["MONGODB_URI"],
            "mongodb://127.0.0.1:27018",
        )

    def test_import_of_pre_configuration_export_still_works(self) -> None:
        self._seed_catalog()
        _, payload = self._decrypted_export()
        del payload["configuration"]
        archive_base64 = base64.b64encode(encrypt_payload(payload, PASSWORD)).decode("ascii")

        _reset_catalog()

        import_response = self._restore(archive_base64)
        self.assertEqual(import_response.status_code, 200)
        self.assertEqual(import_response.json()["configuration"], {})
        self.assertEqual(import_response.json()["restored"]["backups"], 1)

    def test_import_rejects_payload_from_incompatible_future_format(self) -> None:
        bad_payload = {"manifest": {"format_version": 999}, "catalog": {}}
        archive_base64 = base64.b64encode(
            encrypt_payload(bad_payload, PASSWORD)
        ).decode("ascii")

        response = self._restore(archive_base64)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "incompatible_export")

    def test_import_rejects_structurally_invalid_payload(self) -> None:
        archive_base64 = base64.b64encode(
            encrypt_payload({"not": "an export"}, PASSWORD)
        ).decode("ascii")

        response = self._restore(archive_base64)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_export")

    def test_import_rejects_wrong_password_with_a_clean_error(self) -> None:
        self._seed_catalog()
        archive_base64 = self._export(password="right-password").json()["archive_base64"]

        response = self._restore(archive_base64, password="totally-wrong-password")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "wrong_password")

    def test_import_rejects_archive_base64_that_is_not_valid_base64(self) -> None:
        response = self._restore("not-valid-base64!!!")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_export")

    def test_export_without_password_is_rejected(self) -> None:
        response = self.client.post("/api/v1/system/catalog-snapshot", json={})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_argument")

    def test_export_with_a_short_password_is_rejected(self) -> None:
        for password in ("a", "1234", "passwor", "7chars!"):
            with self.subTest(password=password):
                response = self._export(password=password)

                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "invalid_argument")
                self.assertIn("at least 8 characters", response.json()["error"]["message"])

    def test_export_with_a_password_of_the_minimum_length_is_accepted(self) -> None:
        response = self._export(password="8chars!!")

        self.assertEqual(response.status_code, 200, response.text)

    def test_import_still_opens_an_archive_written_with_a_short_password(self) -> None:
        from python.srv.core.secure_archive import encrypt_payload

        _body, payload = self._decrypted_export()
        archive = encrypt_payload(payload, "a")

        response = self._restore(base64.b64encode(archive).decode("ascii"), password="a")

        self.assertEqual(response.status_code, 200, response.text)

    def test_import_without_password_or_archive_is_rejected(self) -> None:
        response = self.client.post("/api/v1/system/catalog-restore", json={})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_argument")


if __name__ == "__main__":
    unittest.main()
